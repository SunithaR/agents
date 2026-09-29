"""Shared contract every agent on the platform implements. This is the piece
that makes the platform 'agentic' rather than just 'an app that calls an LLM
sometimes' -- every capability that reasons over an LLM is expressed as an
Agent, registered once, and invoked uniformly (see registry.py + AgentRun
persistence in services/*)."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentContext:
    """Ambient info handed to every agent run. Kept intentionally small --
    grow it (e.g. tenant id, request id) as more agents need shared context."""

    related_entity_type: str | None = None
    related_entity_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    success: bool
    output: dict[str, Any]
    # Ordered list of plain-dict steps the agent took -- e.g. one entry per
    # optimizer/evaluator pass. Persisted verbatim to AgentRun.trace so a run
    # can be replayed/audited later.
    trace: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class BaseAgent(ABC):
    name: str
    description: str

    @abstractmethod
    async def run(self, input_data: Any, context: AgentContext) -> AgentResult:
        """Execute the agent. Implementations should never raise for
        expected/business-logic failures -- return AgentResult(success=False,
        error=...) instead, so the caller can persist a failed AgentRun
        without a stack trace turning into a 500."""
        raise NotImplementedError
