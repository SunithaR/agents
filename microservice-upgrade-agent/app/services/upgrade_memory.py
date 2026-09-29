"""Long-term memory for the microservice-upgrade agent, built from the
AgentRun audit trail. Before a run starts, the most recent completed runs
against the same repo are condensed into PriorRunSummary records and handed
to the agent, which surfaces them to the model in its kickoff message (see
prompts.build_kickoff_message).

Deliberately lives in the service layer, not in the agent: the agent stays
free of any DB dependency and just receives plain dataclasses on
UpgradeInput, so it remains trivially testable with scripted inputs.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.agents.upgrade.schemas import PriorRunSummary
from app.models.agent_run import AgentRun, AgentRunStatus

# How many recent runs of this agent to scan when looking for ones that
# match the repo. repo_path lives inside the input_payload JSON, which isn't
# portably queryable across SQLite/Postgres, so matching happens in Python
# over this window.
_SCAN_WINDOW = 200
_MAX_SUMMARY_CHARS = 1_000
_MAX_FAILURE_CHARS = 1_500


def normalize_repo_path(repo_path: str) -> str:
    """Canonical form used to decide whether two runs targeted the same repo,
    so "./svc", "svc/" and "/abs/path/svc" all match."""
    return str(Path(repo_path).expanduser().resolve())


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _last_failed_verification(trace: list | None) -> str | None:
    for event in reversed(trace or []):
        if event.get("type") == "verification" and not event.get("passed"):
            return event.get("summary")
    return None


def _summarize(run: AgentRun) -> PriorRunSummary:
    output = run.output_payload or {}
    payload = run.input_payload or {}
    last_failure = None
    if not output.get("approved"):
        last_failure = _last_failed_verification(run.trace)
    return PriorRunSummary(
        run_id=run.id,
        created_at=run.created_at.isoformat(timespec="minutes") if run.created_at else "",
        target_java_version=str(payload.get("target_java_version", "")),
        target_spring_framework_version=str(payload.get("target_spring_framework_version", "")),
        target_spring_boot_version=str(payload.get("target_spring_boot_version", "")),
        approved=bool(output.get("approved")),
        cycles_used=int(output.get("cycles_used") or 0),
        summary=_tail(output.get("summary") or "", _MAX_SUMMARY_CHARS),
        last_failure=_tail(last_failure, _MAX_FAILURE_CHARS) if last_failure else None,
    )


def load_prior_runs(
    db: Session, *, agent_name: str, repo_path: str, limit: int
) -> list[PriorRunSummary]:
    """Most recent completed runs of `agent_name` against `repo_path`, newest
    first, at most `limit`. Only runs that finished (status=succeeded, i.e.
    the agent completed whether or not the upgrade was approved) are used --
    crashed runs (credential errors, exceptions) carry no useful lesson about
    the repo itself."""
    if limit <= 0:
        return []

    target = normalize_repo_path(repo_path)
    candidates = (
        db.query(AgentRun)
        .filter(AgentRun.agent_name == agent_name, AgentRun.status == AgentRunStatus.succeeded)
        .order_by(AgentRun.id.desc())
        .limit(_SCAN_WINDOW)
    )

    matches: list[PriorRunSummary] = []
    for run in candidates:
        raw_path = (run.input_payload or {}).get("repo_path")
        if not raw_path or normalize_repo_path(raw_path) != target:
            continue
        matches.append(_summarize(run))
        if len(matches) >= limit:
            break
    return matches
