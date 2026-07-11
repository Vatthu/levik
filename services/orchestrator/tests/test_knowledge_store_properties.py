"""Property-based tests for the Knowledge Store subsystem.

Property 30: Context Compression Token Bound
- For any repository knowledge and any max_tokens value, the output of
  get_compressed_context() never exceeds max_tokens.

Run with:
  cd services/orchestrator && .venv/bin/python -m pytest tests/test_knowledge_store_properties.py -v

Validates: Requirements 42.1
"""

from __future__ import annotations

import json
import time

import hypothesis.strategies as st
from hypothesis import given, settings

from vikram_orchestrator.knowledge_store import (
    ContextCompression,
    KnowledgeStore,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def max_tokens_st() -> st.SearchStrategy[int]:
    """Generate max_tokens values between 100 and 10000."""
    return st.integers(min_value=100, max_value=10000)


def module_name_st() -> st.SearchStrategy[str]:
    """Generate plausible module names."""
    segment = st.text(
        min_size=1,
        max_size=20,
        alphabet=st.characters(categories=("L", "N", "Pd")),
    )
    return st.lists(segment, min_size=1, max_size=3).map(lambda parts: ".".join(parts))


def file_path_st() -> st.SearchStrategy[str]:
    """Generate plausible file paths."""
    segment = st.text(
        min_size=1,
        max_size=15,
        alphabet=st.characters(categories=("L", "N", "Pd")),
    )
    return st.lists(segment, min_size=1, max_size=4).map(
        lambda parts: "/".join(parts) + ".py"
    )


def module_graph_st() -> st.SearchStrategy[dict[str, list[str]]]:
    """Generate random module dependency graphs of varying sizes."""
    return st.dictionaries(
        keys=module_name_st(),
        values=st.lists(module_name_st(), min_size=0, max_size=5),
        min_size=0,
        max_size=50,
    )


def interface_summaries_st() -> st.SearchStrategy[dict[str, str]]:
    """Generate random interface summaries (file -> public interface description)."""
    summary_text = st.text(
        min_size=1,
        max_size=200,
        alphabet=st.characters(categories=("L", "N", "P", "Z")),
    )
    return st.dictionaries(
        keys=file_path_st(),
        values=summary_text,
        min_size=0,
        max_size=30,
    )


def objective_st() -> st.SearchStrategy[str]:
    """Generate random task objectives."""
    return st.text(
        min_size=1,
        max_size=100,
        alphabet=st.characters(categories=("L", "N", "Z")),
    )


def repo_path_st() -> st.SearchStrategy[str]:
    """Generate plausible repository paths."""
    return st.text(
        min_size=1,
        max_size=30,
        alphabet=st.characters(categories=("L", "N", "Pd")),
    ).map(lambda s: f"/repos/{s}")


# ---------------------------------------------------------------------------
# Helper: Token counting (matches implementation approximation)
# ---------------------------------------------------------------------------


def count_tokens(context: ContextCompression) -> int:
    """Approximate token count using len(text) // 4.

    Serializes the context compression output and counts tokens using the
    same approximation specified in the design: len(full_text) // 4.
    """
    parts: list[str] = []

    # Module graph contribution
    for module, deps in context.module_graph.items():
        parts.append(f"{module}: {', '.join(deps)}")

    # Interface summaries contribution
    for _file_path, summary in context.interface_summaries.items():
        parts.append(summary)

    # Relevance-ranked excerpts contribution
    for _path, excerpt, _relevance in context.relevance_ranked_excerpts:
        parts.append(excerpt)

    full_text = "\n".join(parts)
    return len(full_text) // 4


# ---------------------------------------------------------------------------
# Helper: Seed a KnowledgeStore with structural summaries for testing
# ---------------------------------------------------------------------------


def _seed_store(
    store: KnowledgeStore,
    repo_path: str,
    module_graph: dict[str, list[str]],
    interface_summaries: dict[str, str],
) -> None:
    """Insert structural summaries directly into the store's database."""
    store._conn.execute(
        "INSERT OR REPLACE INTO structural_summaries "
        "(repo_path, module_graph, interface_summaries, last_generated, file_count_at_generation) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            repo_path,
            json.dumps(module_graph),
            json.dumps(interface_summaries),
            time.time(),
            len(module_graph),
        ),
    )
    store._conn.commit()


# ---------------------------------------------------------------------------
# Property 30: Context Compression Token Bound
# Validates: Requirements 42.1
# ---------------------------------------------------------------------------


class TestContextCompressionTokenBound:
    """For any repository knowledge and any max_tokens value, the output of
    get_compressed_context() never exceeds max_tokens."""

    @given(
        max_tokens=max_tokens_st(),
        module_graph=module_graph_st(),
        interface_summaries=interface_summaries_st(),
        objective=objective_st(),
        repo_path=repo_path_st(),
    )
    @settings(max_examples=300)
    def test_compressed_context_never_exceeds_max_tokens(
        self,
        max_tokens: int,
        module_graph: dict[str, list[str]],
        interface_summaries: dict[str, str],
        objective: str,
        repo_path: str,
    ) -> None:
        """**Validates: Requirements 42.1**

        The compressed context output token count (len(text) // 4) must never
        exceed the specified max_tokens, regardless of input size.
        """
        store = KnowledgeStore()
        _seed_store(store, repo_path, module_graph, interface_summaries)

        result = store.get_compressed_context(repo_path, objective, max_tokens)

        assert result is not None
        token_count = count_tokens(result)
        assert token_count <= max_tokens, (
            f"Compressed context has {token_count} tokens, exceeds max_tokens={max_tokens}. "
            f"Module graph entries: {len(result.module_graph)}, "
            f"Interface summaries: {len(result.interface_summaries)}, "
            f"Excerpts: {len(result.relevance_ranked_excerpts)}"
        )

        store.close()

    @given(
        max_tokens=st.integers(min_value=100, max_value=500),
        module_graph=st.dictionaries(
            keys=module_name_st(),
            values=st.lists(module_name_st(), min_size=2, max_size=5),
            min_size=10,
            max_size=30,
        ),
        interface_summaries=st.dictionaries(
            keys=file_path_st(),
            values=st.text(
                min_size=20,
                max_size=100,
                alphabet=st.characters(categories=("L", "N", "Z")),
            ),
            min_size=5,
            max_size=15,
        ),
        objective=objective_st(),
        repo_path=repo_path_st(),
    )
    @settings(max_examples=200)
    def test_token_bound_with_large_inputs_small_budget(
        self,
        max_tokens: int,
        module_graph: dict[str, list[str]],
        interface_summaries: dict[str, str],
        objective: str,
        repo_path: str,
    ) -> None:
        """**Validates: Requirements 42.1**

        Even when the total available knowledge far exceeds the token budget,
        the compressed output respects the bound.
        """
        store = KnowledgeStore()
        _seed_store(store, repo_path, module_graph, interface_summaries)

        result = store.get_compressed_context(repo_path, objective, max_tokens)

        assert result is not None
        token_count = count_tokens(result)
        assert token_count <= max_tokens, (
            f"With large inputs and small budget ({max_tokens}), got {token_count} tokens. "
            f"Module graph entries: {len(result.module_graph)}, "
            f"Interface summaries: {len(result.interface_summaries)}"
        )

        store.close()

    @given(
        max_tokens=max_tokens_st(),
        repo_path=repo_path_st(),
        objective=objective_st(),
    )
    @settings(max_examples=100)
    def test_empty_knowledge_returns_within_bound(
        self,
        max_tokens: int,
        repo_path: str,
        objective: str,
    ) -> None:
        """**Validates: Requirements 42.1**

        When no knowledge exists for a repository, the output is still within
        bounds (trivially satisfied with empty output).
        """
        store = KnowledgeStore()

        result = store.get_compressed_context(repo_path, objective, max_tokens)

        assert result is not None
        token_count = count_tokens(result)
        assert token_count <= max_tokens, (
            f"Empty knowledge produced {token_count} tokens, exceeds {max_tokens}"
        )
        # With no knowledge, output should be effectively empty
        assert len(result.module_graph) == 0
        assert len(result.interface_summaries) == 0
        assert len(result.relevance_ranked_excerpts) == 0

        store.close()

    @given(
        max_tokens=max_tokens_st(),
        module_graph=module_graph_st(),
        interface_summaries=interface_summaries_st(),
        objective=objective_st(),
        repo_path=repo_path_st(),
    )
    @settings(max_examples=200)
    def test_output_is_valid_context_compression(
        self,
        max_tokens: int,
        module_graph: dict[str, list[str]],
        interface_summaries: dict[str, str],
        objective: str,
        repo_path: str,
    ) -> None:
        """**Validates: Requirements 42.1**

        The output is a valid ContextCompression instance with the correct
        repo_path and all fields properly typed.
        """
        store = KnowledgeStore()
        _seed_store(store, repo_path, module_graph, interface_summaries)

        result = store.get_compressed_context(repo_path, objective, max_tokens)

        assert isinstance(result, ContextCompression)
        assert result.repo_path == repo_path
        assert isinstance(result.module_graph, dict)
        assert isinstance(result.interface_summaries, dict)
        assert isinstance(result.relevance_ranked_excerpts, list)

        # Verify token bound holds
        token_count = count_tokens(result)
        assert token_count <= max_tokens

        store.close()
