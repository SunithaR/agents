"""A tiny in-process registry mapping agent name -> instance. This is what
lets the service advertise its agent roster (GET /agents) and look agents up
by name generically, instead of every route importing a concrete agent class
directly. Register new agents in `bootstrap_agents()`."""
from app.agents.base import BaseAgent

_REGISTRY: dict[str, BaseAgent] = {}


def register_agent(agent: BaseAgent) -> None:
    _REGISTRY[agent.name] = agent


def get_agent(name: str) -> BaseAgent | None:
    return _REGISTRY.get(name)


def list_agents() -> list[BaseAgent]:
    return list(_REGISTRY.values())


def bootstrap_agents() -> None:
    """Called once at app startup. Import agent modules lazily inside this
    function (not at module top-level) to avoid import-order issues between
    the registry and the agents that depend on it."""
    from app.agents.upgrade.agent import MicroserviceUpgradeAgent

    register_agent(MicroserviceUpgradeAgent())
