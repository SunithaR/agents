import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentRunStatus(str, enum.Enum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class AgentRun(Base):
    """Audit trail for every agent invocation on the platform. This is the
    piece that makes the platform 'AI-native' rather than AI-bolted-on: every
    agent call -- not just the translation agent -- is expected to persist
    one of these, so agent behavior is inspectable and reproducible.
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[AgentRunStatus] = mapped_column(Enum(AgentRunStatus), default=AgentRunStatus.running)

    input_payload: Mapped[dict] = mapped_column(JSON)
    output_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Ordered list of {iteration, ...} dicts -- e.g. each optimizer/evaluator
    # pass for the translation agent's evaluator-optimizer loop.
    trace: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional link back to the domain entity this run acted on, e.g.
    # ("job", 42) for a job-description translation.
    related_entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    related_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
