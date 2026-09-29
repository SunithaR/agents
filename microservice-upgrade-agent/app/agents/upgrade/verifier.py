"""Independent, deterministic build + startup verification -- the ground
truth the upgrade loop trusts, not the model's self-report. After the model
calls report_status(ready_for_verification), the agent runs *these*
functions itself and only accepts the upgrade as done when they say so.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

from app.agents.upgrade.schemas import CommandResult
from app.agents.upgrade.tools import run_allowlisted_command

_STARTUP_SUCCESS_RE = re.compile(r"Started \S+ in [\d.]+ seconds?", re.IGNORECASE)
_STARTUP_FAILURE_MARKERS = (
    "APPLICATION FAILED TO START",
    "Exception in thread \"main\"",
    "BUILD FAILURE",
    "Error: Unable to access jarfile",
)


def detect_build_tool(repo_root: Path) -> str | None:
    if (repo_root / "pom.xml").is_file():
        return "maven"
    if (repo_root / "build.gradle").is_file() or (repo_root / "build.gradle.kts").is_file():
        return "gradle"
    return None


def _maven_executable(repo_root: Path) -> str:
    wrapper = repo_root / "mvnw"
    return "./mvnw" if wrapper.is_file() and os.access(wrapper, os.X_OK) else "mvn"


def _gradle_executable(repo_root: Path) -> str:
    wrapper = repo_root / "gradlew"
    return "./gradlew" if wrapper.is_file() and os.access(wrapper, os.X_OK) else "gradle"


def build_command(repo_root: Path, build_tool: str) -> str:
    if build_tool == "maven":
        return f"{_maven_executable(repo_root)} clean package"
    if build_tool == "gradle":
        return f"{_gradle_executable(repo_root)} build"
    raise ValueError(f"Unsupported build tool: {build_tool}")


def startup_command(repo_root: Path, build_tool: str) -> str:
    if build_tool == "maven":
        return f"{_maven_executable(repo_root)} spring-boot:run"
    if build_tool == "gradle":
        return f"{_gradle_executable(repo_root)} bootRun"
    raise ValueError(f"Unsupported build tool: {build_tool}")


def looks_like_startup_success(log_text: str) -> bool:
    return bool(_STARTUP_SUCCESS_RE.search(log_text))


def looks_like_startup_failure(log_text: str) -> bool:
    return any(marker in log_text for marker in _STARTUP_FAILURE_MARKERS)


def run_build(repo_root: Path, timeout_seconds: int) -> CommandResult:
    build_tool = detect_build_tool(repo_root)
    if build_tool is None:
        return CommandResult(
            command="(none)",
            exit_code=None,
            output="No pom.xml or build.gradle[.kts] found at repo root -- not a recognized "
            "Maven or Gradle project.",
            timed_out=False,
            succeeded=False,
        )
    command = build_command(repo_root, build_tool)
    return run_allowlisted_command(repo_root, command, timeout_seconds)


def run_startup_check(repo_root: Path, timeout_seconds: int) -> CommandResult:
    """Starts the app as a real subprocess, tails its output for a Spring
    Boot startup-success or startup-failure marker (or a timeout), then
    terminates it. Uses a new process group (POSIX) so the whole process
    tree -- Maven/Gradle wrapper plus the JVM it spawns -- gets killed, not
    just the wrapper.
    """
    build_tool = detect_build_tool(repo_root)
    if build_tool is None:
        return CommandResult(
            command="(none)", exit_code=None,
            output="No pom.xml or build.gradle[.kts] found -- cannot start the app.",
            timed_out=False, succeeded=False,
        )
    command = startup_command(repo_root, build_tool)
    argv = command.split()  # both commands above are simple, no quoting needed

    proc = subprocess.Popen(
        argv,
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,  # own process group, so we can kill children too
    )

    collected: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    succeeded = False
    failed = False

    try:
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                # Process exited on its own before we saw a clear marker --
                # that's a failure (a healthy Spring Boot app keeps running).
                break
            chunk = proc.stdout.read()
            if chunk:
                collected.append(chunk)
                joined = "".join(collected)
                if looks_like_startup_success(joined):
                    succeeded = True
                    break
                if looks_like_startup_failure(joined):
                    failed = True
                    break
            time.sleep(0.25)
        else:
            failed = True  # loop exhausted the deadline without a verdict -> timeout
    finally:
        _terminate_process_group(proc)

    # Drain whatever's left, non-blocking best-effort.
    try:
        if proc.stdout:
            rest = proc.stdout.read()
            if rest:
                collected.append(rest)
    except Exception:  # noqa: BLE001 -- best-effort drain, never fail the check on this
        pass

    output = "".join(collected)
    timed_out = not succeeded and not failed
    return CommandResult(
        command=command,
        exit_code=proc.returncode,
        output=output,
        timed_out=timed_out,
        succeeded=succeeded,
    )


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # already gone
