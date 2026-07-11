"""Property-based tests for the Multi-Repository Coordination subsystem.

These tests define correctness properties for:
- Multi-Repository Verification Aggregation (Property 21)
- Multi-Repository Merge Gate Conjunction (Property 22)

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_multi_repo_properties.py -v

Validates: Requirements 23.3, 23.4, 24.1, 24.2
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings, assume

from vikram_orchestrator.multi_repo import (
    MergeGateCondition,
    MultiRepoTask,
    RepoRef,
    RepoState,
    aggregate_verification,
    evaluate_merge_gate,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def repo_path_st() -> st.SearchStrategy[str]:
    """Generate plausible repository paths."""
    segment = st.text(
        min_size=1,
        max_size=15,
        alphabet=st.characters(categories=("L", "N", "Pd")),
    )
    return st.lists(segment, min_size=1, max_size=4).map(lambda parts: "/".join(parts))


def verification_result_st() -> st.SearchStrategy[str]:
    """Generate verification results: pass or fail."""
    return st.sampled_from(["pass", "fail"])


def repo_results_st(
    min_repos: int = 1, max_repos: int = 8
) -> st.SearchStrategy[dict[str, str]]:
    """Generate a mapping of repo_path -> verification result."""
    return st.dictionaries(
        keys=repo_path_st(),
        values=verification_result_st(),
        min_size=min_repos,
        max_size=max_repos,
    )


def merge_gate_condition_st() -> st.SearchStrategy[MergeGateCondition]:
    """Generate a random MergeGateCondition for a single repo."""
    return st.builds(
        MergeGateCondition,
        repo_path=repo_path_st(),
        verification_passed=st.booleans(),
        review_approved=st.booleans(),
        conflict_free=st.booleans(),
        governance_cleared=st.booleans(),
    )


def merge_gate_conditions_st(
    min_repos: int = 1, max_repos: int = 8
) -> st.SearchStrategy[dict[str, MergeGateCondition]]:
    """Generate per-repo merge gate conditions."""
    return st.dictionaries(
        keys=repo_path_st(),
        values=st.builds(
            lambda v, r, c, g: MergeGateCondition(
                repo_path="placeholder",
                verification_passed=v,
                review_approved=r,
                conflict_free=c,
                governance_cleared=g,
            ),
            v=st.booleans(),
            r=st.booleans(),
            c=st.booleans(),
            g=st.booleans(),
        ),
        min_size=min_repos,
        max_size=max_repos,
    ).map(
        lambda d: {k: MergeGateCondition(repo_path=k, **{
            "verification_passed": v.verification_passed,
            "review_approved": v.review_approved,
            "conflict_free": v.conflict_free,
            "governance_cleared": v.governance_cleared,
        }) for k, v in d.items()}
    )


def task_id_st() -> st.SearchStrategy[str]:
    """Generate plausible task identifiers."""
    return st.text(
        min_size=1,
        max_size=20,
        alphabet=st.characters(categories=("L", "N", "Pd")),
    )


# ---------------------------------------------------------------------------
# Property 21: Multi-Repository Verification Aggregation
# Validates: Requirements 23.3, 23.4
# ---------------------------------------------------------------------------


class TestMultiRepoVerificationAggregation:
    """Aggregate verification passes only when ALL repos pass individually.

    **Validates: Requirements 23.3, 23.4**
    """

    @given(repo_results=repo_results_st())
    @settings(max_examples=300)
    def test_aggregate_passes_iff_all_pass(self, repo_results: dict[str, str]) -> None:
        """**Validates: Requirements 23.3**

        The aggregate verification result is "pass" if and only if every
        individual repo result is "pass".
        """
        overall, per_repo = aggregate_verification(repo_results)

        all_pass = all(r == "pass" for r in repo_results.values())

        if all_pass:
            assert overall == "pass", (
                f"All repos passed but aggregate is '{overall}'. "
                f"Results: {repo_results}"
            )
        else:
            assert overall == "fail", (
                f"Not all repos passed but aggregate is '{overall}'. "
                f"Results: {repo_results}"
            )

    @given(repo_results=repo_results_st())
    @settings(max_examples=200)
    def test_per_repo_results_preserved(self, repo_results: dict[str, str]) -> None:
        """**Validates: Requirements 23.3**

        The per-repo results returned by aggregate_verification contain
        exactly the same repos and results as the input.
        """
        _, per_repo = aggregate_verification(repo_results)

        assert per_repo == repo_results, (
            f"Per-repo results were not preserved. "
            f"Input: {repo_results}, Output: {per_repo}"
        )

    @given(repo_results=repo_results_st(min_repos=1, max_repos=8))
    @settings(max_examples=200)
    def test_single_failure_causes_aggregate_fail(
        self, repo_results: dict[str, str]
    ) -> None:
        """**Validates: Requirements 23.4**

        If any single repository fails verification, the aggregate must fail.
        """
        has_failure = any(r == "fail" for r in repo_results.values())

        overall, _ = aggregate_verification(repo_results)

        if has_failure:
            assert overall == "fail", (
                f"At least one repo failed but aggregate is '{overall}'. "
                f"Results: {repo_results}"
            )

    @given(
        passing_repos=st.dictionaries(
            keys=repo_path_st(),
            values=st.just("pass"),
            min_size=1,
            max_size=7,
        ),
        failing_repo=repo_path_st(),
    )
    @settings(max_examples=200)
    def test_adding_failing_repo_breaks_aggregate(
        self, passing_repos: dict[str, str], failing_repo: str
    ) -> None:
        """**Validates: Requirements 23.4**

        Starting with all passing repos, adding a single failing repo causes
        the aggregate to fail.
        """
        assume(failing_repo not in passing_repos)

        # All pass initially
        overall_before, _ = aggregate_verification(passing_repos)
        assert overall_before == "pass"

        # Add a failing repo
        combined = {**passing_repos, failing_repo: "fail"}
        overall_after, _ = aggregate_verification(combined)
        assert overall_after == "fail", (
            f"Adding failing repo '{failing_repo}' did not cause aggregate to fail. "
            f"Combined: {combined}"
        )

    def test_empty_repo_results_passes(self) -> None:
        """**Validates: Requirements 23.3**

        An empty set of repos vacuously passes verification.
        """
        overall, per_repo = aggregate_verification({})
        assert overall == "pass"
        assert per_repo == {}

    @given(
        task_id=task_id_st(),
        data=st.data(),
    )
    @settings(max_examples=200)
    def test_multi_repo_task_aggregate_matches_standalone(
        self, task_id: str, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 23.3**

        The MultiRepoTask.aggregate_verification() method produces the same
        result as the standalone aggregate_verification() function.
        """
        # Generate repos
        repo_paths = data.draw(
            st.lists(repo_path_st(), min_size=1, max_size=6, unique=True)
        )
        results = {
            rp: data.draw(verification_result_st()) for rp in repo_paths
        }

        # Set up MultiRepoTask
        refs = [RepoRef(repo_path=rp) for rp in repo_paths]
        task = MultiRepoTask(task_id=task_id, repos=refs)
        for rp, result in results.items():
            task.repo_states[rp].verification_result = result

        # Compare
        task_overall, task_per_repo = task.aggregate_verification()
        standalone_overall, _ = aggregate_verification(results)

        assert task_overall == standalone_overall, (
            f"MultiRepoTask aggregate '{task_overall}' != standalone '{standalone_overall}'. "
            f"Results: {results}"
        )

    @given(
        task_id=task_id_st(),
        data=st.data(),
    )
    @settings(max_examples=150)
    def test_detached_repos_excluded_from_aggregate(
        self, task_id: str, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 23.3**

        Detached repos are not considered in the aggregation.
        A task with one failing repo that is then detached should pass if
        remaining repos all pass.
        """
        repo_paths = data.draw(
            st.lists(repo_path_st(), min_size=2, max_size=6, unique=True)
        )

        # Make all pass except the first one
        refs = [RepoRef(repo_path=rp) for rp in repo_paths]
        task = MultiRepoTask(task_id=task_id, repos=refs)

        # First repo fails
        task.repo_states[repo_paths[0]].verification_result = "fail"
        for rp in repo_paths[1:]:
            task.repo_states[rp].verification_result = "pass"

        # Before detach: should fail
        overall_before, _ = task.aggregate_verification()
        assert overall_before == "fail"

        # Detach the failing repo
        task.detach_repo(repo_paths[0])

        # After detach: should pass (remaining all pass)
        overall_after, per_repo_after = task.aggregate_verification()
        assert overall_after == "pass", (
            f"After detaching failing repo, aggregate should pass. "
            f"Per-repo: {per_repo_after}"
        )
        assert repo_paths[0] not in per_repo_after


# ---------------------------------------------------------------------------
# Property 22: Multi-Repository Merge Gate Conjunction
# Validates: Requirements 24.1, 24.2
# ---------------------------------------------------------------------------


class TestMultiRepoMergeGateConjunction:
    """Multi-repo merge gate passes only when ALL repos satisfy their
    individual gate conditions.

    **Validates: Requirements 24.1, 24.2**
    """

    @given(conditions=merge_gate_conditions_st())
    @settings(max_examples=300)
    def test_merge_gate_passes_iff_all_repos_pass(
        self, conditions: dict[str, MergeGateCondition]
    ) -> None:
        """**Validates: Requirements 24.1**

        The merge gate passes if and only if every repo's gate conditions
        are all satisfied.
        """
        passes, blockers = evaluate_merge_gate(conditions)

        all_repos_pass = all(c.passes for c in conditions.values())

        if all_repos_pass:
            assert passes is True, (
                f"All repos passed gate but overall is blocked. "
                f"Conditions: {conditions}"
            )
            assert blockers == [], (
                f"All repos passed but blockers is non-empty: {blockers}"
            )
        else:
            assert passes is False, (
                f"Not all repos passed but overall passed. "
                f"Conditions: {conditions}"
            )
            assert len(blockers) > 0, (
                f"Gate failed but no blockers reported."
            )

    @given(conditions=merge_gate_conditions_st(min_repos=1, max_repos=8))
    @settings(max_examples=200)
    def test_blockers_are_exactly_failing_repos(
        self, conditions: dict[str, MergeGateCondition]
    ) -> None:
        """**Validates: Requirements 24.2**

        The blockers list contains exactly the repos that do NOT pass their
        individual gate conditions.
        """
        _, blockers = evaluate_merge_gate(conditions)

        expected_blockers = {
            repo_path for repo_path, cond in conditions.items()
            if not cond.passes
        }

        assert set(blockers) == expected_blockers, (
            f"Blockers mismatch. Expected: {expected_blockers}, Got: {set(blockers)}"
        )

    @given(data=st.data())
    @settings(max_examples=200)
    def test_single_condition_failure_blocks_gate(
        self, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 24.1**

        If a single condition in a single repo is False, the entire merge
        gate fails.
        """
        repo_paths = data.draw(
            st.lists(repo_path_st(), min_size=1, max_size=6, unique=True)
        )

        # Start with all conditions True for all repos
        conditions: dict[str, MergeGateCondition] = {
            rp: MergeGateCondition(
                repo_path=rp,
                verification_passed=True,
                review_approved=True,
                conflict_free=True,
                governance_cleared=True,
            )
            for rp in repo_paths
        }

        # Initially should pass
        passes_before, _ = evaluate_merge_gate(conditions)
        assert passes_before is True

        # Flip one condition on one repo
        target_repo = data.draw(st.sampled_from(repo_paths))
        field_to_flip = data.draw(
            st.sampled_from([
                "verification_passed",
                "review_approved",
                "conflict_free",
                "governance_cleared",
            ])
        )

        # Create a modified condition for the target repo
        modified_cond = conditions[target_repo].model_copy(
            update={field_to_flip: False}
        )
        conditions[target_repo] = modified_cond

        # Should now fail
        passes_after, blockers = evaluate_merge_gate(conditions)
        assert passes_after is False, (
            f"Flipping '{field_to_flip}' on repo '{target_repo}' should block gate. "
            f"Conditions: {conditions}"
        )
        assert target_repo in blockers, (
            f"Repo '{target_repo}' should be in blockers after flipping '{field_to_flip}'"
        )

    @given(
        conditions=st.dictionaries(
            keys=repo_path_st(),
            values=st.just(
                MergeGateCondition(
                    repo_path="placeholder",
                    verification_passed=True,
                    review_approved=True,
                    conflict_free=True,
                    governance_cleared=True,
                )
            ),
            min_size=1,
            max_size=8,
        ).map(
            lambda d: {k: MergeGateCondition(
                repo_path=k,
                verification_passed=True,
                review_approved=True,
                conflict_free=True,
                governance_cleared=True,
            ) for k in d}
        )
    )
    @settings(max_examples=200)
    def test_all_conditions_true_always_passes(
        self, conditions: dict[str, MergeGateCondition]
    ) -> None:
        """**Validates: Requirements 24.1**

        When all individual conditions are True for all repos, the gate
        always passes.
        """
        passes, blockers = evaluate_merge_gate(conditions)
        assert passes is True, (
            f"All conditions True but gate failed. Blockers: {blockers}"
        )
        assert blockers == []

    def test_empty_conditions_passes(self) -> None:
        """**Validates: Requirements 24.1**

        An empty set of repo conditions vacuously passes the merge gate.
        """
        passes, blockers = evaluate_merge_gate({})
        assert passes is True
        assert blockers == []

    @given(
        task_id=task_id_st(),
        data=st.data(),
    )
    @settings(max_examples=200)
    def test_multi_repo_task_merge_gate_matches_standalone(
        self, task_id: str, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 24.1**

        The MultiRepoTask.evaluate_merge_gate(conditions) method produces
        the same result as the standalone evaluate_merge_gate() function.
        """
        repo_paths = data.draw(
            st.lists(repo_path_st(), min_size=1, max_size=6, unique=True)
        )

        conditions: dict[str, MergeGateCondition] = {}
        for rp in repo_paths:
            conditions[rp] = MergeGateCondition(
                repo_path=rp,
                verification_passed=data.draw(st.booleans()),
                review_approved=data.draw(st.booleans()),
                conflict_free=data.draw(st.booleans()),
                governance_cleared=data.draw(st.booleans()),
            )

        # Set up MultiRepoTask
        refs = [RepoRef(repo_path=rp) for rp in repo_paths]
        task = MultiRepoTask(task_id=task_id, repos=refs)

        # Compare
        task_passes, task_blockers = task.evaluate_merge_gate(conditions)
        standalone_passes, standalone_blockers = evaluate_merge_gate(conditions)

        assert task_passes == standalone_passes, (
            f"MultiRepoTask gate '{task_passes}' != standalone '{standalone_passes}'"
        )
        assert set(task_blockers) == set(standalone_blockers), (
            f"MultiRepoTask blockers {task_blockers} != standalone {standalone_blockers}"
        )

    @given(
        task_id=task_id_st(),
        data=st.data(),
    )
    @settings(max_examples=150)
    def test_detached_repos_excluded_from_merge_gate(
        self, task_id: str, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 24.2**

        Detached repos are not considered in merge gate evaluation.
        A task with one blocking repo that is then detached should pass
        if remaining repos all satisfy their conditions.
        """
        repo_paths = data.draw(
            st.lists(repo_path_st(), min_size=2, max_size=6, unique=True)
        )

        refs = [RepoRef(repo_path=rp) for rp in repo_paths]
        task = MultiRepoTask(task_id=task_id, repos=refs)

        # All repos pass conditions except the first
        conditions: dict[str, MergeGateCondition] = {}
        for rp in repo_paths:
            conditions[rp] = MergeGateCondition(
                repo_path=rp,
                verification_passed=True,
                review_approved=True,
                conflict_free=True,
                governance_cleared=True,
            )
        # First repo is blocked
        conditions[repo_paths[0]] = MergeGateCondition(
            repo_path=repo_paths[0],
            verification_passed=False,
            review_approved=True,
            conflict_free=True,
            governance_cleared=True,
        )

        # Before detach: should fail
        passes_before, blockers_before = task.evaluate_merge_gate(conditions)
        assert passes_before is False
        assert repo_paths[0] in blockers_before

        # Detach the blocking repo
        task.detach_repo(repo_paths[0])

        # After detach: should pass
        passes_after, blockers_after = task.evaluate_merge_gate(conditions)
        assert passes_after is True, (
            f"After detaching blocking repo, gate should pass. "
            f"Blockers: {blockers_after}"
        )
        assert repo_paths[0] not in blockers_after

    @given(data=st.data())
    @settings(max_examples=200)
    def test_merge_gate_condition_passes_requires_all_four_fields(
        self, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 24.1**

        A MergeGateCondition.passes is True only when all four conditions
        (verification_passed, review_approved, conflict_free, governance_cleared)
        are True.
        """
        v = data.draw(st.booleans())
        r = data.draw(st.booleans())
        c = data.draw(st.booleans())
        g = data.draw(st.booleans())

        cond = MergeGateCondition(
            repo_path="test/repo",
            verification_passed=v,
            review_approved=r,
            conflict_free=c,
            governance_cleared=g,
        )

        expected = v and r and c and g
        assert cond.passes == expected, (
            f"MergeGateCondition.passes={cond.passes} but expected "
            f"{expected} for v={v}, r={r}, c={c}, g={g}"
        )
