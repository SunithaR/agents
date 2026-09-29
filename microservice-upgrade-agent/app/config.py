"""Central settings. Loaded once from environment / .env and imported everywhere
as `from app.config import settings`."""
import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Populate the real process environment from .env, not just this Settings
# model. This matters for Anthropic auth: the SDK resolves credentials
# (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, an `ant auth login` profile, or
# Workload Identity Federation env vars) by reading os.environ directly --
# pydantic-settings' own env_file loading below only fills *this* model's
# fields, it does not export anything to os.environ. Without this call, a
# key set only in .env (never exported in the shell) would silently never
# reach the SDK. override=False so already-exported shell/CI env vars win.
load_dotenv(override=False)

# Footgun guard: an env var present but set to "" still occupies its slot in
# the SDK's credential precedence (ANTHROPIC_API_KEY -> ANTHROPIC_AUTH_TOKEN
# -> ant auth login profile -> WIF) and can shadow a working `ant auth login`
# profile with an empty, invalid key -- `ant auth status` warns about exactly
# this. A stale ".env" line like `ANTHROPIC_API_KEY=` (no value) is enough to
# trigger it. Strip empty values so an unset-looking line behaves as unset.
for _credential_var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
    if os.environ.get(_credential_var) == "":
        del os.environ[_credential_var]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"

    # --- LLM / Anthropic -----------------------------------------------
    # No API-key field here on purpose: the service doesn't require one.
    # The Anthropic SDK resolves credentials itself, in order --
    # ANTHROPIC_API_KEY -> ANTHROPIC_AUTH_TOKEN -> the active `ant auth
    # login` profile -> Workload Identity Federation env vars. Run
    # `ant auth login` and you need nothing in .env at all. See
    # app/agents/auth.py for a diagnostic that reports which of these is
    # actually active.
    anthropic_model: str = "claude-opus-5"
    # Forces the agent onto a mock/scripted LLM regardless of credentials.
    # Used by tests; also handy for local dev without any credentials set up.
    llm_mock_mode: bool = False

    # --- Database ---------------------------------------------------------
    database_url: str = "sqlite:///./microservice_upgrade_agent.db"

    # --- Microservice upgrade agent (tool-use + build/startup verification loop) --
    upgrade_agent_model: str | None = None
    # xhigh is Anthropic's own recommendation for coding/agentic workloads --
    # this agent edits real source files and drives real builds, squarely in
    # that category.
    upgrade_agent_effort: str = "xhigh"
    # Outer loop: each cycle is "let the model work, then independently verify
    # with a real build + startup attempt". This caps how many times we'll
    # feed verification failures back to the model before giving up.
    upgrade_max_verification_cycles: int = 5
    # Inner loop: caps tool-call turns within a single cycle so a confused
    # model can't loop forever between verifications.
    upgrade_max_tool_turns_per_cycle: int = 25
    upgrade_build_timeout_seconds: int = 900
    upgrade_startup_timeout_seconds: int = 90
    upgrade_command_timeout_seconds: int = 300
    # Long-term memory: how many earlier completed runs against the same repo
    # are summarized into the kickoff message (see services/upgrade_memory.py).
    # 0 disables cross-run memory entirely.
    upgrade_memory_max_prior_runs: int = 3

    @property
    def upgrade_model(self) -> str:
        return self.upgrade_agent_model or self.anthropic_model


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
