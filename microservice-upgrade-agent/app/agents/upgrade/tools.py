"""Client-executed tools for the microservice-upgrade agent: file read/write/
edit, directory listing, and a confined shell-command runner. These are
regular custom tools (JSON schema + Python function), not Anthropic's
built-in bash/text-editor tools -- custom tools let us enforce path
confinement and a command allowlist centrally, which a generic bash tool
would not (see shared/agent-design.md's "promote to a dedicated tool when you
need to gate/validate/audit" guidance).

Every tool is confined to the repo root passed to ToolExecutor -- no reads,
writes, or commands can escape it (see _resolve_within_root). This agent
still edits real files and runs real commands within that root; run it only
against repositories you own/trust, ideally under version control so changes
are reviewable and revertible (git status/diff/checkout).
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import asdict
from pathlib import Path

from app.agents.upgrade.schemas import CommandResult

MAX_FILE_READ_CHARS = 20_000
MAX_TOOL_RESULT_CHARS = 8_000
MAX_LIST_ENTRIES = 500

# Executables the model may invoke via run_command. Deliberately excludes
# shell interpreters (bash/sh/python/etc.) so the allowlist can't be
# trivially bypassed, and excludes anything destructive/networked (rm, mv,
# curl, wget, chmod, sudo, ssh) that a build/startup workflow doesn't need.
ALLOWED_EXECUTABLES = {
    "mvn", "mvnw", "./mvnw",
    "gradle", "gradlew", "./gradlew",
    "java", "javac", "jar",
    "git",
    "ls", "cat", "find", "grep", "sed", "wc", "head", "tail", "pwd", "echo", "true",
}


class PathEscapesRepoRoot(ValueError):
    pass


class CommandNotAllowed(ValueError):
    pass


def _resolve_within_root(root: Path, raw_path: str) -> Path:
    """Resolve a model-supplied path (relative or absolute) and verify it
    stays within `root`. Mirrors the text-editor-tool security guidance:
    resolve to canonical form, reject traversal (.., symlinks, absolute
    paths outside the root)."""
    candidate = Path(raw_path)
    combined = candidate if candidate.is_absolute() else root / candidate
    resolved = combined.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathEscapesRepoRoot(f"Path '{raw_path}' resolves outside the repo root")
    return resolved


def _truncate(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} more characters]"


TOOL_SCHEMAS: list[dict] = [
    {
        "name": "read_file",
        "description": (
            "Read a text file from the repository. Path is relative to the repository root "
            "(or an absolute path inside it). Large files are truncated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path to read"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create or overwrite a file with the given content. Creates parent directories "
            "if needed. Use for new files or full-file rewrites; prefer edit_file for "
            "small, targeted changes to an existing file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace one exact occurrence of old_str with new_str in an existing file. "
            "Fails if old_str appears zero or more than once -- make old_str long enough "
            "to be unique (include surrounding context lines)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories under a path in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "recursive": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a build/inspection command inside the repository root (e.g. `mvn -v`, "
            "`./mvnw clean compile`, `git status`, `grep -r spring-boot-starter pom.xml`). "
            "Only a fixed allowlist of executables is permitted (build tools, java, git, "
            "and basic read-only Unix utilities) -- no shell interpreters, no destructive "
            "or networked commands. No shell operators (&&, |, ;, backticks) are "
            "interpreted -- the command is split into argv and run directly, so each "
            "invocation is a single program call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "e.g. 'mvn clean package -DskipTests=false'",
                },
                "timeout_seconds": {"type": "integer", "default": 300},
            },
            "required": ["command"],
        },
    },
    {
        "name": "report_status",
        "description": (
            "Call this when you believe the upgrade is complete and ready for an "
            "independent build+startup verification, OR when you are stuck and cannot "
            "proceed without human input. This ends your current turn of work; if "
            "verification fails, you'll be given the failure details and continue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["ready_for_verification", "blocked"]},
                "summary": {
                    "type": "string",
                    "description": "What you changed, or what's blocking you",
                },
            },
            "required": ["status", "summary"],
        },
    },
]

# Tools whose execution is meaningful file/process activity, as opposed to
# report_status which is pure loop-control and has no side effect to run.
_EXECUTABLE_TOOLS = {"read_file", "write_file", "edit_file", "list_directory", "run_command"}


class ToolExecutor:
    def __init__(self, repo_root: Path, command_timeout_seconds: int) -> None:
        self.repo_root = repo_root
        self.default_command_timeout = command_timeout_seconds

    def execute(self, tool_name: str, tool_input: dict) -> tuple[str, bool]:
        """Returns (result_text, is_error). Never raises -- tool failures are
        reported back to the model as an error tool_result so it can adapt,
        matching the pattern in shared/tool-use-concepts.md."""
        if tool_name not in _EXECUTABLE_TOOLS:
            return f"Unknown tool: {tool_name}", True
        try:
            handler = getattr(self, f"_{tool_name}")
            return handler(tool_input), False
        except (PathEscapesRepoRoot, CommandNotAllowed) as exc:
            return str(exc), True
        except FileNotFoundError as exc:
            return f"File not found: {exc}", True
        except Exception as exc:  # noqa: BLE001 -- surfaced to the model, not raised
            return f"Tool execution failed: {exc}", True

    def _read_file(self, data: dict) -> str:
        path = _resolve_within_root(self.repo_root, data["path"])
        if not path.is_file():
            raise FileNotFoundError(data["path"])
        content = path.read_text(errors="replace")
        return _truncate(content, MAX_FILE_READ_CHARS)

    def _write_file(self, data: dict) -> str:
        path = _resolve_within_root(self.repo_root, data["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data["content"])
        return f"Wrote {len(data['content'])} characters to {data['path']}"

    def _edit_file(self, data: dict) -> str:
        path = _resolve_within_root(self.repo_root, data["path"])
        if not path.is_file():
            raise FileNotFoundError(data["path"])
        content = path.read_text()
        old_str, new_str = data["old_str"], data["new_str"]
        count = content.count(old_str)
        if count == 0:
            return f"old_str not found in {data['path']} -- no changes made"
        if count > 1:
            return (
                f"old_str appears {count} times in {data['path']} -- must be unique. "
                "Include more surrounding context and retry."
            )
        path.write_text(content.replace(old_str, new_str, 1))
        return f"Edited {data['path']}"

    def _list_directory(self, data: dict) -> str:
        path = _resolve_within_root(self.repo_root, data.get("path", "."))
        if not path.is_dir():
            raise FileNotFoundError(data.get("path", "."))
        pattern = "**/*" if data.get("recursive") else "*"
        entries = sorted(path.glob(pattern))[:MAX_LIST_ENTRIES]
        lines = [
            f"{'d' if e.is_dir() else 'f'} {e.relative_to(self.repo_root)}" for e in entries
        ]
        if not lines:
            return "(empty directory)"
        return "\n".join(lines)

    def _run_command(self, data: dict) -> str:
        result = run_allowlisted_command(
            self.repo_root,
            data["command"],
            timeout_seconds=data.get("timeout_seconds") or self.default_command_timeout,
        )
        return _truncate(
            f"exit_code={result.exit_code} timed_out={result.timed_out}\n{result.output}"
        )


def run_allowlisted_command(repo_root: Path, command: str, timeout_seconds: int) -> CommandResult:
    """Runs `command` with shell=False (argv split via shlex, so shell
    operators like &&, |, ;, backticks are never interpreted) and cwd pinned
    to repo_root. Raises CommandNotAllowed if the executable isn't
    allowlisted."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise CommandNotAllowed(f"Could not parse command: {exc}") from exc
    if not argv:
        raise CommandNotAllowed("Empty command")
    executable = argv[0]
    if executable not in ALLOWED_EXECUTABLES:
        raise CommandNotAllowed(
            f"'{executable}' is not an allowed command. Allowed: {sorted(ALLOWED_EXECUTABLES)}"
        )

    try:
        proc = subprocess.run(
            argv,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return CommandResult(
            command=command,
            exit_code=proc.returncode,
            output=output,
            timed_out=False,
            succeeded=proc.returncode == 0,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            command=command,
            exit_code=None,
            output=f"Command timed out after {timeout_seconds}s",
            timed_out=True,
            succeeded=False,
        )


def command_result_dict(result: CommandResult) -> dict:
    d = asdict(result)
    d["output"] = _truncate(d["output"])
    return d
