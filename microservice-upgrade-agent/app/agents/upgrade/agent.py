"""MicroserviceUpgradeAgent: upgrades a Java/Spring microservice (Java 8->21,
Spring Framework 4->6, Spring Boot 1.5->3.5 by default) given a path to its
code. Rather than an evaluator-optimizer pattern (an LLM judging another
LLM's output), this agent's "evaluator" is a deterministic
build + startup check (verifier.py) -- the only trustworthy signal that an
upgrade actually works is whether the service builds and starts, not a
model's opinion of its own diff. The loop is:

    for each verification cycle (up to a cap):
        let the model work with tools (read/write/edit files, run build
        commands) until it calls report_status
        independently run a real build, then a real startup attempt
        if both pass -> done
        else -> feed the failure output back to the model and try again

This agent executes real shell commands and edits real files under the given
repo_path (see tools.py for the path-confinement and command-allowlist
safeguards). Run it only against repositories you own/trust, ideally under
version control so changes are reviewable and revertible.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Callable

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.agents.upgrade import verifier as verifier_module
from app.agents.upgrade.llm import AnthropicToolCallLLM, ToolCallLLM
from app.agents.upgrade.prompts import (
    build_kickoff_message,
    build_system_prompt,
    build_verification_feedback_message,
)
from app.agents.upgrade.schemas import (
    CommandResult,
    ModelNote,
    ToolCallRecord,
    UpgradeInput,
    UpgradeTrace,
    VerificationResult,
)
from app.agents.upgrade.tools import TOOL_SCHEMAS, ToolExecutor
from app.config import settings

_MAX_TOOL_RESULT_IN_TRACE = 2_000
_LOG_TAIL_CHARS = 4_000

RunBuildFn = Callable[[Path, int], CommandResult]
RunStartupFn = Callable[[Path, int], CommandResult]


def _tail(text: str, limit: int = _LOG_TAIL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"...[showing last {limit} of {len(text)} characters]\n" + text[-limit:]


class MicroserviceUpgradeAgent(BaseAgent):
    name = "microservice_upgrade"
    description = (
        "Upgrades a Java/Spring microservice given a path to its code (default targets: "
        "Java 21, Spring Framework 6, Spring Boot 3.5). Uses file read/write/edit and build "
        "tools to make the changes, then independently runs a real build and startup "
        "attempt and iterates on the model's own work until both succeed or an iteration "
        "cap is hit -- the pass/fail signal is a real build+startup, not the model's "
        "self-report."
    )

    def __init__(
        self,
        llm: ToolCallLLM | None = None,
        run_build: RunBuildFn | None = None,
        run_startup_check: RunStartupFn | None = None,
    ) -> None:
        self._llm = llm or AnthropicToolCallLLM()
        # Injectable for tests -- the real functions shell out to mvn/gradle
        # and start a JVM, which the test suite should never do.
        self._run_build = run_build or verifier_module.run_build
        self._run_startup_check = run_startup_check or verifier_module.run_startup_check
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def run(self, input_data: UpgradeInput, context: AgentContext) -> AgentResult:
        repo_root = Path(input_data.repo_path)
        if not repo_root.is_dir():
            return AgentResult(
                success=False,
                output={},
                error=f"repo_path does not exist or is not a directory: {input_data.repo_path}",
            )

        build_tool = verifier_module.detect_build_tool(repo_root)
        if build_tool is None:
            return AgentResult(
                success=False,
                output={},
                error=(
                    f"No pom.xml or build.gradle[.kts] found at {input_data.repo_path} -- "
                    "not a recognized Maven or Gradle project."
                ),
            )

        max_cycles = input_data.max_verification_cycles or settings.upgrade_max_verification_cycles
        max_turns = input_data.max_tool_turns_per_cycle or settings.upgrade_max_tool_turns_per_cycle
        build_timeout = input_data.build_timeout_seconds or settings.upgrade_build_timeout_seconds
        startup_timeout = input_data.startup_timeout_seconds or settings.upgrade_startup_timeout_seconds

        executor = ToolExecutor(repo_root, command_timeout_seconds=settings.upgrade_command_timeout_seconds)
        system_prompt = build_system_prompt(
            target_java_version=input_data.target_java_version,
            target_spring_framework_version=input_data.target_spring_framework_version,
            target_spring_boot_version=input_data.target_spring_boot_version,
        )
        messages: list[dict] = [
            {
                "role": "user",
                "content": build_kickoff_message(
                    repo_root=str(repo_root), prior_runs=input_data.prior_runs
                ),
            }
        ]

        # Which earlier runs were shown to the model, so a run's output records
        # what memory it started from.
        prior_run_ids = [r.run_id for r in input_data.prior_runs]

        trace = UpgradeTrace()
        blocked_summary: str | None = None

        try:
            for cycle in range(1, max_cycles + 1):
                turn_result = await self._run_inner_loop(
                    messages=messages,
                    system_prompt=system_prompt,
                    executor=executor,
                    trace=trace,
                    cycle=cycle,
                    max_turns=max_turns,
                )

                if turn_result["status"] == "blocked":
                    blocked_summary = turn_result["summary"]
                    break

                verification = self._verify(repo_root, cycle, build_timeout, startup_timeout)
                trace.verifications.append(verification)

                if verification.passed:
                    return AgentResult(
                        success=True,
                        output={
                            "approved": True,
                            "cycles_used": cycle,
                            "build_tool": build_tool,
                            "summary": verification.summary,
                            "prior_run_ids": prior_run_ids,
                        },
                        trace=self._serialize_trace(trace),
                    )

                if cycle < max_cycles:
                    messages.append(
                        {
                            "role": "user",
                            "content": build_verification_feedback_message(
                                cycle=cycle, summary=verification.summary
                            ),
                        }
                    )
        except Exception as exc:  # noqa: BLE001 -- surfaced as a failed AgentRun, not a 500
            return AgentResult(
                success=False, output={}, trace=self._serialize_trace(trace), error=str(exc)
            )

        final_summary = blocked_summary or (
            f"Upgrade did not pass build+startup verification within {max_cycles} cycle(s)."
        )
        return AgentResult(
            success=True,  # the agent completed without crashing; "approved" says whether it worked
            output={
                "approved": False,
                "cycles_used": len(trace.verifications),
                "build_tool": build_tool,
                "summary": final_summary,
                "prior_run_ids": prior_run_ids,
            },
            trace=self._serialize_trace(trace),
        )

    async def _run_inner_loop(
        self,
        *,
        messages: list[dict],
        system_prompt: str,
        executor: ToolExecutor,
        trace: UpgradeTrace,
        cycle: int,
        max_turns: int,
    ) -> dict:
        """Drives tool-use turns until the model calls report_status or the
        per-cycle turn cap is hit. Mutates `messages` (the shared
        conversation history) and `trace` in place."""
        for _turn in range(1, max_turns + 1):
            response = await self._llm.create_message(
                model=settings.upgrade_model,
                system=system_prompt,
                messages=messages,
                tools=TOOL_SCHEMAS,
                max_tokens=8192,
                effort=settings.upgrade_agent_effort,
            )

            assistant_content: list[dict] = []
            tool_uses = []
            for block in response.content:
                if block.type == "text":
                    if block.text.strip():
                        trace.model_notes.append(
                            ModelNote(seq=self._next_seq(), cycle=cycle, text=block.text)
                        )
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    tool_uses.append(block)
                    assistant_content.append(
                        {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                    )
            messages.append({"role": "assistant", "content": assistant_content})

            if not tool_uses:
                # No tool calls at all -- nudge rather than silently stalling;
                # report_status is the only structured way to end a cycle.
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You didn't call any tools. If you're finished with this round of "
                            "changes, call report_status. Otherwise continue using tools."
                        ),
                    }
                )
                continue

            report_status_call = next((tu for tu in tool_uses if tu.name == "report_status"), None)

            tool_results: list[dict] = []
            for tu in tool_uses:
                if tu.name == "report_status":
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": tu.id, "content": "Acknowledged."}
                    )
                    continue
                result_text, is_error = executor.execute(tu.name, tu.input)
                trace.tool_calls.append(
                    ToolCallRecord(
                        seq=self._next_seq(),
                        cycle=cycle,
                        tool=tu.name,
                        input=tu.input,
                        result_summary=result_text[:_MAX_TOOL_RESULT_IN_TRACE],
                        is_error=is_error,
                    )
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_text,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

            if report_status_call is not None:
                return {
                    "status": report_status_call.input.get("status", "ready_for_verification"),
                    "summary": report_status_call.input.get("summary", ""),
                }

        return {
            "status": "ready_for_verification",  # give it a real verification rather than silently giving up
            "summary": f"(turn limit reached after {max_turns} turns without calling report_status)",
        }

    def _verify(
        self, repo_root: Path, cycle: int, build_timeout: int, startup_timeout: int
    ) -> VerificationResult:
        build_result = self._run_build(repo_root, build_timeout)
        if not build_result.succeeded:
            summary = (
                f"Build failed (exit_code={build_result.exit_code}, "
                f"timed_out={build_result.timed_out}).\n\n{_tail(build_result.output)}"
            )
            return VerificationResult(
                seq=self._next_seq(), cycle=cycle, build=build_result, startup=None,
                passed=False, summary=summary,
            )

        startup_result = self._run_startup_check(repo_root, startup_timeout)
        if not startup_result.succeeded:
            reason = "timed out waiting for a startup marker" if startup_result.timed_out else "exited before starting successfully"
            summary = f"Build succeeded, but the application {reason}.\n\n{_tail(startup_result.output)}"
            return VerificationResult(
                seq=self._next_seq(), cycle=cycle, build=build_result, startup=startup_result,
                passed=False, summary=summary,
            )

        return VerificationResult(
            seq=self._next_seq(), cycle=cycle, build=build_result, startup=startup_result,
            passed=True, summary="Build succeeded and the application started successfully.",
        )

    @staticmethod
    def _serialize_trace(trace: UpgradeTrace) -> list[dict]:
        events = (
            [{"type": "tool_call", **asdict(t)} for t in trace.tool_calls]
            + [{"type": "model_note", **asdict(n)} for n in trace.model_notes]
            + [{"type": "verification", **asdict(v)} for v in trace.verifications]
        )
        events.sort(key=lambda e: e["seq"])
        return events
