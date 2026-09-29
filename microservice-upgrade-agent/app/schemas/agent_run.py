from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.agent_run import AgentRunStatus


class AgentRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_name: str
    status: AgentRunStatus
    input_payload: dict
    output_payload: dict | None
    trace: list | None
    error_message: str | None
    related_entity_type: str | None
    related_entity_id: int | None
    created_at: datetime
    completed_at: datetime | None


class AgentInfo(BaseModel):
    """What GET /agents advertises about a registered agent."""

    name: str
    description: str
