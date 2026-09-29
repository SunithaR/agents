"""Internal dataclasses for the microservice-upgrade agent. Separate from
app/schemas/upgrade.py (the API-facing Pydantic models), same split as the
translation agent."""
from dataclasses import dataclass, field


@dataclass
class UpgradeInput:
    repo_path: str
    target_java_version: str = "21"
    target_spring_framework_version: str = "6"
    target_spring_boot_version: str = "3.5"
    max_verification_cycles: int | None = None
    max_tool_turns_per_cycle: int | None = None
    build_timeout_seconds: int | None = None
    startup_timeout_seconds: int | None = None


@dataclass
class ToolCallRecord:
    """One tool call the model made and what it got back. Truncated results
    are stored, not raw file contents/build logs in full, to keep AgentRun
    rows a reasonable size -- see tools.py's truncation helpers."""

    seq: int
    cycle: int
    tool: str
    input: dict
    result_summary: str
    is_error: bool


@dataclass
class ModelNote:
    """Any plain-text the model wrote alongside its tool calls -- kept for
    audit/debugging, not used to drive control flow (report_status is)."""

    seq: int
    cycle: int
    text: str


@dataclass
class CommandResult:
    command: str
    exit_code: int | None
    output: str
    timed_out: bool
    succeeded: bool


@dataclass
class VerificationResult:
    """The outcome of one independent build+startup check -- the ground
    truth the loop trusts, not the model's self-report."""

    seq: int
    cycle: int
    build: CommandResult | None
    startup: CommandResult | None
    passed: bool
    summary: str


@dataclass
class UpgradeTrace:
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    model_notes: list[ModelNote] = field(default_factory=list)
    verifications: list[VerificationResult] = field(default_factory=list)
