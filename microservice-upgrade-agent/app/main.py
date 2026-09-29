import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agents.auth import describe_credentials
from app.agents.registry import bootstrap_agents
from app.api import routes_agents, routes_upgrade
from app.config import settings
from app.db import init_db

logger = logging.getLogger("microservice_upgrade_agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bootstrap_agents()

    if settings.llm_mock_mode:
        logger.info("LLM_MOCK_MODE is on -- the upgrade agent will not call the Anthropic API")
    else:
        creds = describe_credentials()
        if creds.configured:
            logger.info("Anthropic credentials resolved via %s (%s)", creds.source, creds.detail)
        else:
            logger.warning(
                "%s The app will still start; the upgrade agent will fail at request time "
                "until credentials are available.",
                creds.detail,
            )

    yield


app = FastAPI(
    title="Microservice Upgrade Agent",
    description=(
        "A standalone agentic service that upgrades a Java/Spring microservice given a path "
        "to its code (see GET /agents) -- a genuine tool-use agent that reads, edits, and "
        "builds real files, verified by an actual build+startup check rather than the "
        "model's self-report. Every invocation is persisted for audit (see GET /agent-runs)."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(routes_upgrade.router)
app.include_router(routes_agents.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/llm", tags=["meta"])
def health_llm() -> dict[str, str | bool]:
    """Reports which Anthropic credential source is active, without
    revealing any secret value -- useful for confirming `ant auth login`
    (or an API key, or WIF) actually took effect before hitting /upgrade.
    Note: this endpoint has no auth of its own (see README) -- it discloses
    only *whether* and *how* credentials are configured, never the secret."""
    if settings.llm_mock_mode:
        return {"configured": True, "source": "mock", "detail": "LLM_MOCK_MODE is on"}
    creds = describe_credentials()
    return {"configured": creds.configured, "source": creds.source, "detail": creds.detail}
