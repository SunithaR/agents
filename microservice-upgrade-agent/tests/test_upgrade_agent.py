from pathlib import Path

import pytest

from app.agents.base import AgentContext
from app.agents.upgrade.agent import MicroserviceUpgradeAgent
from app.agents.upgrade.llm import FakeMessage, FakeTextBlock, FakeToolUseBlock, ScriptedToolCallLLM
from app.agents.upgrade.prompts import build_kickoff_message
from app.agents.upgrade.schemas import CommandResult, PriorRunSummary, UpgradeInput
from app.agents.upgrade.tools import (
    CommandNotAllowed,
    PathEscapesRepoRoot,
    ToolExecutor,
    _resolve_within_root,
    run_allowlisted_command,
)
from app.agents.upgrade.verifier import (
    build_command,
    detect_build_tool,
    looks_like_startup_failure,
    looks_like_startup_success,
    startup_command,
)
from app.db import SessionLocal
from app.models.agent_run import AgentRun, AgentRunStatus
from app.services.upgrade_memory import load_prior_runs


# --- Path confinement -----------------------------------------------------


def test_resolve_within_root_allows_relative_path(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    resolved = _resolve_within_root(tmp_path, "pom.xml")
    assert resolved == (tmp_path / "pom.xml").resolve()


def test_resolve_within_root_rejects_traversal(tmp_path):
    with pytest.raises(PathEscapesRepoRoot):
        _resolve_within_root(tmp_path, "../outside.txt")


def test_resolve_within_root_rejects_absolute_path_outside_root(tmp_path):
    with pytest.raises(PathEscapesRepoRoot):
        _resolve_within_root(tmp_path, "/etc/passwd")


# --- ToolExecutor: filesystem tools ----------------------------------------


def test_write_then_read_file(tmp_path):
    executor = ToolExecutor(tmp_path, command_timeout_seconds=30)
    result, is_error = executor.execute("write_file", {"path": "src/App.java", "content": "class App {}"})
    assert is_error is False
    assert (tmp_path / "src" / "App.java").read_text() == "class App {}"

    result, is_error = executor.execute("read_file", {"path": "src/App.java"})
    assert is_error is False
    assert result == "class App {}"


def test_edit_file_requires_unique_match(tmp_path):
    (tmp_path / "pom.xml").write_text("<version>8</version>\n<version>8</version>")
    executor = ToolExecutor(tmp_path, command_timeout_seconds=30)

    result, is_error = executor.execute(
        "edit_file", {"path": "pom.xml", "old_str": "<version>8</version>", "new_str": "<version>21</version>"}
    )
    assert is_error is False  # not a tool *error* -- it's a reported ambiguity
    assert "appears 2 times" in result


def test_edit_file_applies_unique_replacement(tmp_path):
    (tmp_path / "pom.xml").write_text("<java.version>8</java.version>")
    executor = ToolExecutor(tmp_path, command_timeout_seconds=30)

    result, is_error = executor.execute(
        "edit_file",
        {"path": "pom.xml", "old_str": "<java.version>8</java.version>", "new_str": "<java.version>21</java.version>"},
    )
    assert is_error is False
    assert (tmp_path / "pom.xml").read_text() == "<java.version>21</java.version>"


def test_read_file_escaping_root_is_rejected(tmp_path):
    executor = ToolExecutor(tmp_path, command_timeout_seconds=30)
    result, is_error = executor.execute("read_file", {"path": "../../etc/passwd"})
    assert is_error is True


def test_list_directory(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.java").write_text("")
    (tmp_path / "pom.xml").write_text("")
    executor = ToolExecutor(tmp_path, command_timeout_seconds=30)

    result, is_error = executor.execute("list_directory", {"path": ".", "recursive": True})
    assert is_error is False
    assert "pom.xml" in result
    assert "App.java" in result


# --- Command allowlist ------------------------------------------------------


def test_run_command_rejects_disallowed_executable(tmp_path):
    with pytest.raises(CommandNotAllowed):
        run_allowlisted_command(tmp_path, "rm -rf /", timeout_seconds=10)


def test_run_command_rejects_shell_interpreters(tmp_path):
    with pytest.raises(CommandNotAllowed):
        run_allowlisted_command(tmp_path, "bash -c 'echo hi'", timeout_seconds=10)


def test_run_command_runs_allowlisted_executable(tmp_path):
    result = run_allowlisted_command(tmp_path, "git --version", timeout_seconds=10)
    assert result.succeeded is True
    assert "git version" in result.output.lower()


# --- Build-tool detection + startup log parsing ----------------------------


def test_detect_build_tool_maven(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    assert detect_build_tool(tmp_path) == "maven"


def test_detect_build_tool_gradle(tmp_path):
    (tmp_path / "build.gradle").write_text("")
    assert detect_build_tool(tmp_path) == "gradle"


def test_detect_build_tool_none(tmp_path):
    assert detect_build_tool(tmp_path) is None


def test_build_command_prefers_wrapper_when_present_and_executable(tmp_path):
    wrapper = tmp_path / "mvnw"
    wrapper.write_text("#!/bin/sh\n")
    wrapper.chmod(0o755)
    assert build_command(tmp_path, "maven").startswith("./mvnw")


def test_build_command_falls_back_to_bare_executable(tmp_path):
    assert build_command(tmp_path, "maven") == "mvn clean package"


def test_startup_command_gradle():
    result = startup_command(Path("/tmp/nonexistent"), "gradle")
    assert "bootRun" in result


def test_looks_like_startup_success():
    assert looks_like_startup_success("2026-08-10 INFO Started DemoApplication in 3.214 seconds")
    assert not looks_like_startup_success("Building...")


def test_looks_like_startup_failure():
    assert looks_like_startup_failure("***\nAPPLICATION FAILED TO START\n***")
    assert not looks_like_startup_failure("Started DemoApplication in 2.1 seconds")


# --- Full agent loop, LLM and verifier both faked --------------------------


def _project_dir(tmp_path) -> Path:
    (tmp_path / "pom.xml").write_text("<project/>")
    return tmp_path


def _report_status_turn(status: str, summary: str = "done") -> FakeMessage:
    return FakeMessage(
        content=[
            FakeTextBlock(text="Working on it."),
            FakeToolUseBlock(id="call-1", name="report_status", input={"status": status, "summary": summary}),
        ],
        stop_reason="tool_use",
    )


def _tool_use_turn(name: str, tool_input: dict) -> FakeMessage:
    return FakeMessage(
        content=[FakeToolUseBlock(id="call-x", name=name, input=tool_input)], stop_reason="tool_use"
    )


@pytest.mark.asyncio
async def test_agent_succeeds_on_first_cycle_when_verification_passes(tmp_path):
    repo = _project_dir(tmp_path)
    llm = ScriptedToolCallLLM(
        turns=[
            _tool_use_turn("write_file", {"path": "pom.xml", "content": "<project><version>21</version></project>"}),
            _report_status_turn("ready_for_verification"),
        ]
    )

    def fake_build(_repo: Path, _timeout: int) -> CommandResult:
        return CommandResult(command="mvn clean package", exit_code=0, output="BUILD SUCCESS", timed_out=False, succeeded=True)

    def fake_startup(_repo: Path, _timeout: int) -> CommandResult:
        return CommandResult(
            command="mvn spring-boot:run", exit_code=0,
            output="Started DemoApplication in 2.0 seconds", timed_out=False, succeeded=True,
        )

    agent = MicroserviceUpgradeAgent(llm=llm, run_build=fake_build, run_startup_check=fake_startup)
    result = await agent.run(UpgradeInput(repo_path=str(repo)), AgentContext())

    assert result.success is True
    assert result.output["approved"] is True
    assert result.output["cycles_used"] == 1
    assert result.output["build_tool"] == "maven"
    # The write_file tool call and the verification should both be in the trace.
    tool_call_events = [e for e in result.trace if e["type"] == "tool_call"]
    verification_events = [e for e in result.trace if e["type"] == "verification"]
    assert len(tool_call_events) == 1
    assert len(verification_events) == 1
    assert verification_events[0]["passed"] is True


@pytest.mark.asyncio
async def test_agent_retries_after_failed_verification_then_succeeds(tmp_path):
    repo = _project_dir(tmp_path)
    llm = ScriptedToolCallLLM(
        turns=[
            _tool_use_turn("write_file", {"path": "pom.xml", "content": "attempt 1"}),
            _report_status_turn("ready_for_verification", "first attempt"),
            _tool_use_turn("write_file", {"path": "pom.xml", "content": "attempt 2"}),
            _report_status_turn("ready_for_verification", "second attempt"),
        ]
    )

    build_calls = {"n": 0}

    def fake_build(_repo: Path, _timeout: int) -> CommandResult:
        build_calls["n"] += 1
        if build_calls["n"] == 1:
            return CommandResult(command="mvn clean package", exit_code=1, output="COMPILE ERROR", timed_out=False, succeeded=False)
        return CommandResult(command="mvn clean package", exit_code=0, output="BUILD SUCCESS", timed_out=False, succeeded=True)

    def fake_startup(_repo: Path, _timeout: int) -> CommandResult:
        return CommandResult(
            command="mvn spring-boot:run", exit_code=0,
            output="Started DemoApplication in 2.0 seconds", timed_out=False, succeeded=True,
        )

    agent = MicroserviceUpgradeAgent(llm=llm, run_build=fake_build, run_startup_check=fake_startup)
    result = await agent.run(UpgradeInput(repo_path=str(repo), max_verification_cycles=5), AgentContext())

    assert result.success is True
    assert result.output["approved"] is True
    assert result.output["cycles_used"] == 2
    assert build_calls["n"] == 2
    verification_events = [e for e in result.trace if e["type"] == "verification"]
    assert verification_events[0]["passed"] is False
    assert "COMPILE ERROR" in verification_events[0]["summary"]
    assert verification_events[1]["passed"] is True


@pytest.mark.asyncio
async def test_agent_stops_when_model_reports_blocked(tmp_path):
    repo = _project_dir(tmp_path)
    llm = ScriptedToolCallLLM(turns=[_report_status_turn("blocked", "need a human decision on X")])

    build_calls = {"n": 0}

    def fake_build(_repo: Path, _timeout: int) -> CommandResult:
        build_calls["n"] += 1
        return CommandResult(command="mvn clean package", exit_code=0, output="", timed_out=False, succeeded=True)

    agent = MicroserviceUpgradeAgent(llm=llm, run_build=fake_build, run_startup_check=fake_build)
    result = await agent.run(UpgradeInput(repo_path=str(repo)), AgentContext())

    assert result.success is True
    assert result.output["approved"] is False
    assert "need a human decision on X" in result.output["summary"]
    assert build_calls["n"] == 0  # blocked -- never even attempted verification


@pytest.mark.asyncio
async def test_agent_still_verifies_when_turn_cap_reached_without_report_status(tmp_path):
    repo = _project_dir(tmp_path)
    # Only one scripted turn, and it never calls report_status -- with
    # max_tool_turns_per_cycle=1 the inner loop exhausts its cap immediately.
    llm = ScriptedToolCallLLM(turns=[_tool_use_turn("read_file", {"path": "pom.xml"})])

    def fake_build(_repo: Path, _timeout: int) -> CommandResult:
        return CommandResult(command="mvn clean package", exit_code=0, output="BUILD SUCCESS", timed_out=False, succeeded=True)

    def fake_startup(_repo: Path, _timeout: int) -> CommandResult:
        return CommandResult(
            command="mvn spring-boot:run", exit_code=0,
            output="Started DemoApplication in 1.0 seconds", timed_out=False, succeeded=True,
        )

    agent = MicroserviceUpgradeAgent(llm=llm, run_build=fake_build, run_startup_check=fake_startup)
    result = await agent.run(
        UpgradeInput(repo_path=str(repo), max_tool_turns_per_cycle=1, max_verification_cycles=1), AgentContext()
    )

    # Even though the model never called report_status, we still ran a real
    # verification rather than silently giving up.
    verification_events = [e for e in result.trace if e["type"] == "verification"]
    assert len(verification_events) == 1
    assert result.output["approved"] is True


@pytest.mark.asyncio
async def test_agent_rejects_nonexistent_repo_path():
    agent = MicroserviceUpgradeAgent(llm=ScriptedToolCallLLM(turns=[]))
    result = await agent.run(UpgradeInput(repo_path="/definitely/does/not/exist"), AgentContext())

    assert result.success is False
    assert "does not exist" in result.error


@pytest.mark.asyncio
async def test_agent_rejects_repo_without_recognized_build_tool(tmp_path):
    agent = MicroserviceUpgradeAgent(llm=ScriptedToolCallLLM(turns=[]))
    result = await agent.run(UpgradeInput(repo_path=str(tmp_path)), AgentContext())

    assert result.success is False
    assert "pom.xml" in result.error


def test_agents_endpoint_lists_upgrade_agent(client):
    resp = client.get("/agents")
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert "microservice_upgrade" in names


# --- Long-term memory (earlier runs against the same repo) -----------------


def _prior_run(**overrides) -> PriorRunSummary:
    fields = dict(
        run_id=7, created_at="2026-09-01T10:00", target_java_version="21",
        target_spring_framework_version="6", target_spring_boot_version="3.5",
        approved=False, cycles_used=5, summary="Did not pass verification.",
        last_failure="cannot find symbol: javax.persistence.Entity",
    )
    fields.update(overrides)
    return PriorRunSummary(**fields)


def test_kickoff_message_without_memory_has_no_memory_section():
    assert "Memory from previous" not in build_kickoff_message(repo_root="/repo")


def test_kickoff_message_includes_prior_runs():
    message = build_kickoff_message(repo_root="/repo", prior_runs=[_prior_run()])
    assert "Memory from previous upgrade attempts" in message
    assert "Run #7" in message
    assert "NOT approved" in message
    assert "javax.persistence.Entity" in message


@pytest.mark.asyncio
async def test_agent_shows_prior_runs_to_model_and_records_them(tmp_path):
    repo = _project_dir(tmp_path)
    llm = ScriptedToolCallLLM(turns=[_report_status_turn("blocked", "stuck")])
    agent = MicroserviceUpgradeAgent(llm=llm)
    result = await agent.run(
        UpgradeInput(repo_path=str(repo), prior_runs=[_prior_run(run_id=3)]), AgentContext()
    )

    assert "Run #3" in llm.calls[0]["kickoff"]
    assert result.output["prior_run_ids"] == [3]


def _add_run(db, *, repo_path, status=AgentRunStatus.succeeded, approved=False, agent_name="microservice_upgrade", trace=None):
    run = AgentRun(
        agent_name=agent_name,
        status=status,
        input_payload={"repo_path": repo_path, "target_java_version": "21",
                       "target_spring_framework_version": "6", "target_spring_boot_version": "3.5"},
        output_payload={"approved": approved, "cycles_used": 2, "summary": f"approved={approved}"},
        trace=trace,
    )
    db.add(run)
    db.commit()
    return run


def test_load_prior_runs_matches_repo_newest_first_and_skips_crashed_runs(tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    failed_trace = [
        {"type": "verification", "passed": False, "summary": "early failure"},
        {"type": "verification", "passed": False, "summary": "COMPILE ERROR in Foo.java"},
    ]
    with SessionLocal() as db:
        older = _add_run(db, repo_path=str(repo), trace=failed_trace)
        _add_run(db, repo_path=str(tmp_path / "other"))
        _add_run(db, repo_path=str(repo), status=AgentRunStatus.failed)
        _add_run(db, repo_path=str(repo), agent_name="some_other_agent")
        # Same repo spelled differently -- must still match.
        newer = _add_run(db, repo_path=str(repo) + "/", approved=True)

        prior = load_prior_runs(db, agent_name="microservice_upgrade", repo_path=str(repo), limit=5)

    assert [p.run_id for p in prior] == [newer.id, older.id]
    assert prior[0].approved is True and prior[0].last_failure is None
    assert prior[1].last_failure == "COMPILE ERROR in Foo.java"


def test_load_prior_runs_respects_limit(tmp_path):
    with SessionLocal() as db:
        for _ in range(3):
            _add_run(db, repo_path=str(tmp_path))
        assert len(load_prior_runs(db, agent_name="microservice_upgrade", repo_path=str(tmp_path), limit=2)) == 2
        assert load_prior_runs(db, agent_name="microservice_upgrade", repo_path=str(tmp_path), limit=0) == []
