"""Orchestrates a microservice-upgrade agent invocation and persists it as an
AgentRun."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agents.base import AgentContext
from app.agents.registry import get_agent
from app.agents.upgrade.schemas import UpgradeInput
from app.config import settings
from app.models.agent_run import AgentRun, AgentRunStatus
from app.schemas.upgrade import UpgradeRequest
from app.services.upgrade_memory import load_prior_runs


class UpgradeAgentUnavailable(RuntimeError):
    """Raised if the upgrade agent was never registered (bootstrap_agents()
    not called) -- should only happen if the app is mis-wired."""


async def run_upgrade(db: Session, request: UpgradeRequest) -> AgentRun:
    agent = get_agent("microservice_upgrade")
    if agent is None:
        raise UpgradeAgentUnavailable("microservice_upgrade agent is not registered")

    # Loaded before this run's own AgentRun row exists, so it can never
    # appear in its own memory.
    prior_runs = (
        load_prior_runs(
            db,
            agent_name=agent.name,
            repo_path=request.repo_path,
            limit=settings.upgrade_memory_max_prior_runs,
        )
        if request.use_memory
        else []
    )

    input_data = UpgradeInput(
        repo_path=request.repo_path,
        target_java_version=request.target_java_version,
        target_spring_framework_version=request.target_spring_framework_version,
        target_spring_boot_version=request.target_spring_boot_version,
        max_verification_cycles=request.max_verification_cycles,
        max_tool_turns_per_cycle=request.max_tool_turns_per_cycle,
        build_timeout_seconds=request.build_timeout_seconds,
        startup_timeout_seconds=request.startup_timeout_seconds,
        prior_runs=prior_runs,
    )

    run = AgentRun(
        agent_name=agent.name,
        status=AgentRunStatus.running,
        input_payload=request.model_dump(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    result = await agent.run(input_data, AgentContext())

    run.trace = result.trace
    run.completed_at = datetime.now(timezone.utc)
    if result.success:
        run.status = AgentRunStatus.succeeded
        run.output_payload = result.output
    else:
        run.status = AgentRunStatus.failed
        run.error_message = result.error

    db.commit()
    db.refresh(run)
    return run
