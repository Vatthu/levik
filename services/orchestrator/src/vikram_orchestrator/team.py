from __future__ import annotations

from collections.abc import Iterable

from .models import AgentProfile, AgentThinkRequest


class AgentUnavailableError(Exception):
    """Raised when no suitable agent can be found for the requested role."""

    def __init__(self, role: str, available_alternatives: list[str]) -> None:
        self.role = role
        self.available_alternatives = available_alternatives
        alternatives_str = ", ".join(available_alternatives) if available_alternatives else "none"
        super().__init__(
            f"No agent available for role '{role}'. "
            f"Available alternatives: [{alternatives_str}]"
        )


class AgentCallFailedError(Exception):
    """Raised when all retries and fallbacks for an agent call are exhausted."""

    def __init__(self, role: str, attempts: int, last_error: Exception | None) -> None:
        self.role = role
        self.attempts = attempts
        self.last_error = last_error
        last_err_str = str(last_error) if last_error else "unknown"
        super().__init__(
            f"Agent call failed for role '{role}' after {attempts} attempt(s). "
            f"Last error: {last_err_str}"
        )


class TeamRouter:
    """Selects an available model route from host-provided team metadata."""

    def __init__(self, agents: Iterable[AgentProfile]) -> None:
        self._agents = list(agents)

    @classmethod
    def from_state(cls, roster: object) -> TeamRouter:
        agents: list[AgentProfile] = []
        if isinstance(roster, list):
            for item in roster:
                try:
                    agents.append(AgentProfile.model_validate(item))
                except Exception:
                    continue
        return cls(agents)

    def request(self, task_id: str, role: str, prompt: str) -> AgentThinkRequest:
        try:
            selected = self._select(role)
        except AgentUnavailableError:
            return AgentThinkRequest(task_id=task_id, role=role, prompt=prompt)

        return AgentThinkRequest(
            task_id=task_id,
            role=selected.role or role,
            prompt=prompt,
            provider=selected.provider,
            model=selected.model,
        )

    def select_with_fallback(
        self, primary_role: str, fallback_roles: list[str]
    ) -> AgentProfile:
        """Attempt to select an agent for primary_role, falling back through
        fallback_roles in order. Raises AgentUnavailableError if all fail."""
        try:
            return self._select(primary_role)
        except AgentUnavailableError:
            pass

        for fallback in fallback_roles:
            try:
                return self._select(fallback)
            except AgentUnavailableError:
                continue

        raise AgentUnavailableError(
            role=primary_role,
            available_alternatives=[a.role for a in self._agents],
        )

    def _select(self, role: str) -> AgentProfile:
        """Select an agent for the given role.

        Resolution order:
        1. Exact role match (case-insensitive, stripped) — returns immediately.
        2. Capability match — scores all agents whose capabilities include the
           role, and returns the best match.
        3. No match — raises AgentUnavailableError with diagnostic info.
        """
        normalized = role.strip().lower()
        if not normalized:
            raise AgentUnavailableError(
                role=role,
                available_alternatives=[a.role for a in self._agents],
            )

        # 1. Exact role match (PRESERVED — same behavior as before)
        for agent in self._agents:
            if agent.role.strip().lower() == normalized:
                return agent

        # 2. Scored capability matching
        capability_matches: list[tuple[float, AgentProfile]] = []
        for agent in self._agents:
            capabilities = {cap.strip().lower() for cap in agent.capabilities}
            if normalized in capabilities:
                # Score: base score of 1.0; could be enhanced with health/load
                # info if the roster provides it in the future.
                score = 1.0
                capability_matches.append((score, agent))

        if capability_matches:
            # Sort by score descending; stable sort preserves insertion order for ties
            capability_matches.sort(key=lambda x: x[0], reverse=True)
            return capability_matches[0][1]

        # 3. No match at all
        raise AgentUnavailableError(
            role=role,
            available_alternatives=[a.role for a in self._agents],
        )
