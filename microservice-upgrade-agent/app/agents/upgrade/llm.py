"""Tool-calling LLM client for the upgrade agent. Separate from
app/agents/llm_client.py's LLMClient (complete_text/complete_json) because
tool use needs raw multi-turn message history and content-block inspection
that a single-shot text/JSON interface can't express -- this agent drives a
manual agentic loop (see agent.py) rather than one-shot completions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic


class ToolCallLLM(Protocol):
    async def create_message(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        effort: str,
    ) -> Any:
        """Returns an object with `.content` (list of content blocks, each
        with `.type` and, depending on type, `.text` or `.id`/`.name`/`.input`)
        and `.stop_reason` -- the same shape as anthropic.types.Message.
        Duck-typed deliberately so tests can supply lightweight fakes (see
        FakeMessage/ScriptedToolCallLLM below) without constructing real SDK
        response objects."""
        ...


class AnthropicToolCallLLM:
    """Real implementation. Zero-arg AsyncAnthropic() -- see
    app/agents/auth.py for how credentials are resolved; no API key required
    if `ant auth login` has been run."""

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic()

    async def create_message(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        effort: str,
    ) -> Any:
        return await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            output_config={"effort": effort},
        )


# --- Test double -----------------------------------------------------------
# Lightweight duck-typed stand-ins for anthropic.types content blocks /
# Message, plus a client that plays back a fixed script of turns. Used by
# tests to exercise the manual agentic loop deterministically, without
# constructing real SDK response objects or making network calls.


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class FakeMessage:
    content: list[FakeTextBlock | FakeToolUseBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"


class ScriptedToolCallLLM:
    """Returns pre-built FakeMessage turns in order, one per call. Queue one
    FakeMessage per expected model turn (including turns across multiple
    verification cycles) before running the agent."""

    def __init__(self, turns: list[FakeMessage]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []

    async def create_message(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        effort: str,
    ) -> FakeMessage:
        self.calls.append(
            {"model": model, "message_count": len(messages), "kickoff": messages[0]["content"]}
        )
        if not self._turns:
            raise AssertionError(
                f"ScriptedToolCallLLM ran out of scripted turns after {len(self.calls)} calls"
            )
        return self._turns.pop(0)
