"""Platform-level agent introspection: what agents are registered, and the
full audit trail (AgentRun) for any past invocation of any of them. This is
what makes the agent registry more than an internal implementation detail --
agent behavior is inspectable from the outside."""
from fastapi import APIRouter, HTTPException

from app.agents.registry import list_agents
from app.api.deps import DbSession
from app.models.agent_run import AgentRun
from app.schemas.agent_run import AgentInfo, AgentRunRead

router = APIRouter(tags=["agents"])


@router.get("/agents", response_model=list[AgentInfo])
def get_agents() -> list[AgentInfo]:
    return [AgentInfo(name=agent.name, description=agent.description) for agent in list_agents()]


@router.get("/agent-runs", response_model=list[AgentRunRead])
def list_agent_runs(db: DbSession, skip: int = 0, limit: int = 50) -> list[AgentRunRead]:
    return list(
        db.query(AgentRun).order_by(AgentRun.id.desc()).offset(skip).limit(limit)
    )


@router.get("/agent-runs/{run_id}", response_model=AgentRunRead)
def get_agent_run(run_id: int, db: DbSession) -> AgentRunRead:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run
